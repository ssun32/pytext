#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

from typing import List

from pytext.config.component import ComponentType, create_component
from pytext.data.tensorizers import TokenTensorizer
from pytext.data.tokenizers import Tokenizer, WordPieceTokenizer
from pytext.data.utils import (
    BOS,
    EOS,
    MASK,
    PAD,
    UNK,
    VocabBuilder,
    Vocabulary,
    pad_and_tensorize,
)


class SquadTensorizer(TokenTensorizer):
    """Produces inputs and answer spans for Squad."""

    SPAN_PAD_IDX = -100

    class Config(TokenTensorizer.Config):
        # for model inputs
        doc_column: str = "doc"
        ques_column: str = "question"
        # for labels
        answers_column: str = "answers"
        answer_starts_column: str = "answer_starts"
        # Since Tokenizer is __EXPANSIBLE__, we don't need a Union type to
        # support WordPieceTokenizer.
        tokenizer: Tokenizer.Config = Tokenizer.Config(split_regex=r"\W+")
        max_ques_seq_len: int = 64
        max_doc_seq_len: int = 256

    @classmethod
    def from_config(cls, config: Config, **kwargs):
        tokenizer = create_component(ComponentType.TOKENIZER, config.tokenizer)
        vocab = None
        if isinstance(tokenizer, WordPieceTokenizer):
            print("Using WordPieceTokenizer")
            replacements = {
                "[UNK]": UNK,
                "[PAD]": PAD,
                "[CLS]": BOS,
                "[SEP]": EOS,
                "[MASK]": MASK,
            }
            vocab = Vocabulary(
                [token for token, _ in tokenizer.vocab.items()],
                replacements=replacements,
            )

        doc_tensorizer = TokenTensorizer(
            text_column=config.doc_column,
            tokenizer=tokenizer,
            vocab=vocab,
            max_seq_len=config.max_doc_seq_len,
        )
        ques_tensorizer = TokenTensorizer(
            text_column=config.ques_column,
            tokenizer=tokenizer,
            vocab=vocab,
            max_seq_len=config.max_ques_seq_len,
        )
        return cls(
            doc_tensorizer,
            ques_tensorizer,
            doc_column=config.doc_column,
            ques_column=config.ques_column,
            answers_column=config.answers_column,
            answer_starts_column=config.answer_starts_column,
            tokenizer=tokenizer,
            vocab=vocab,
            **kwargs,
        )

    def __init__(
        self,
        doc_tensorizer: TokenTensorizer,
        ques_tensorizer: TokenTensorizer,
        doc_column: str = Config.doc_column,
        ques_column: str = Config.ques_column,
        answers_column: str = Config.answers_column,
        answer_starts_column: str = Config.answer_starts_column,
        **kwargs,
    ):
        super().__init__(text_column=None, **kwargs)
        self.ques_tensorizer = ques_tensorizer
        self.doc_tensorizer = doc_tensorizer
        self.doc_column = doc_column
        self.ques_column = ques_column
        self.answers_column = answers_column
        self.answer_starts_column = answer_starts_column

    def initialize(self, vocab_builder=None):
        """Build vocabulary based on training corpus."""
        if not self.vocab:
            self.vocab_builder = vocab_builder or VocabBuilder()
            self.vocab_builder.pad_index = 0
            self.vocab_builder.unk_index = 1
            ques_initializer = self.ques_tensorizer.initialize(self.vocab_builder)
            doc_initializer = self.doc_tensorizer.initialize(self.vocab_builder)
            ques_initializer.send(None)
            doc_initializer.send(None)
        try:
            while True:
                if self.vocab:
                    yield
                else:
                    row = yield
                    ques_initializer.send(row)
                    doc_initializer.send(row)
        except GeneratorExit:
            if not self.vocab:
                self.vocab = self.vocab_builder.make_vocab()

    def _lookup_tokens(self, text, source_is_doc=True):
        # This is useful in SquadMetricReporter._unnumberize()
        return (
            self.doc_tensorizer._lookup_tokens(text)
            if source_is_doc
            else self.ques_tensorizer._lookup_tokens(text)
        )

    def numberize(self, row):
        assert len(self.vocab) == len(self.ques_tensorizer.vocab)
        assert len(self.vocab) == len(self.doc_tensorizer.vocab)

        # Do NOT use self._lookup_tokens() because it won't enforce max_ques_seq_len.
        ques_tokens, _, _ = self.ques_tensorizer._lookup_tokens(row[self.ques_column])

        # Start and end indices are those of the tokens in original text.
        # The behavior doesn't change for WordPieceTokenizer because...
        # If there's a word piece, say, "##ly" then the start and end indices
        # will be that of "ly" in original text. These are also char level.
        doc_tokens, orig_start_idx, orig_end_idx = self.doc_tensorizer._lookup_tokens(
            row[self.doc_column]
        )

        # Now map original character level answer spans to token level spans
        start_idx_map = {}
        end_idx_map = {}
        for token_idx, (start_idx, end_idx) in enumerate(
            zip(orig_start_idx, orig_end_idx)
        ):
            start_idx_map[start_idx] = token_idx
            end_idx_map[end_idx] = token_idx

        answer_start_token_indices = [
            start_idx_map.get(raw_idx, self.SPAN_PAD_IDX)
            for raw_idx in row[self.answer_starts_column]
        ]
        answer_end_token_indices = [
            end_idx_map.get(raw_idx + len(answer), self.SPAN_PAD_IDX)
            for raw_idx, answer in zip(
                row[self.answer_starts_column], row[self.answers_column]
            )
        ]  # The end index is inclusive. Span = doc_tokens[start:end+1]

        if (
            not (answer_start_token_indices and answer_end_token_indices)
            or self._only_pad(answer_start_token_indices)
            or self._only_pad(answer_end_token_indices)
        ):
            answer_start_token_indices = [self.SPAN_PAD_IDX]
            answer_end_token_indices = [self.SPAN_PAD_IDX]

        return (
            doc_tokens,
            len(doc_tokens),
            ques_tokens,
            len(ques_tokens),
            answer_start_token_indices,
            answer_end_token_indices,
        )

    def tensorize(self, batch):
        (
            doc_tokens,
            doc_seq_len,
            ques_tokens,
            ques_seq_len,
            answer_start_idx,
            answer_end_idx,
        ) = zip(*batch)
        doc_tokens = pad_and_tensorize(doc_tokens, self.vocab.get_pad_index())
        doc_mask = (doc_tokens == self.vocab.get_pad_index()).byte()  # 1 => pad
        ques_tokens = pad_and_tensorize(ques_tokens, self.vocab.get_pad_index())
        ques_mask = (ques_tokens == self.vocab.get_pad_index()).byte()  # 1 => pad
        answer_start_idx = pad_and_tensorize(answer_start_idx, self.SPAN_PAD_IDX)
        answer_end_idx = pad_and_tensorize(answer_end_idx, self.SPAN_PAD_IDX)

        # doc_tokens must be returned as the first element for
        # SquadMetricReporter._add_decoded_answer_batch_stats() to work
        return (
            doc_tokens,
            pad_and_tensorize(doc_seq_len),
            doc_mask,
            ques_tokens,
            pad_and_tensorize(ques_seq_len),
            ques_mask,
            answer_start_idx,
            answer_end_idx,
        )

    def sort_key(self, row):
        raise NotImplementedError("SquadTensorizer.sort_key() should not be called.")

    def _only_pad(self, token_id_list: List[int]) -> bool:
        for token_id in token_id_list:
            if token_id != self.SPAN_PAD_IDX:
                return False
        return True
