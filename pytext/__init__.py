#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import json
import uuid
from typing import Callable, Mapping, Optional

import numpy as np
from caffe2.python import workspace
from caffe2.python.predictor import predictor_exporter
from pytext.data.tensorizers import (
    FloatListTensorizer,
    GazetteerTensorizer,
    TokenTensorizer,
)
from pytext.task.new_task import NewTask

from .builtin_task import register_builtin_tasks
from .config import PyTextConfig, pytext_config_from_json
from .utils.onnx import CAFFE2_DB_TYPE, convert_caffe2_blob_name


register_builtin_tasks()


Predictor = Callable[[Mapping[str, str]], Mapping[str, np.array]]


def _predict(workspace_id, predict_net, model, tensorizers, input):
    workspace.SwitchWorkspace(workspace_id)
    tensor_dict = {
        name: tensorizer.numberize(input) for name, tensorizer in tensorizers.items()
    }
    model_inputs = model.arrange_model_inputs(tensor_dict)
    model_input_names = model.get_export_input_names(tensorizers)
    for blob_name, model_input in zip(model_input_names, model_inputs):
        converted_blob_name = convert_caffe2_blob_name(blob_name)
        workspace.blobs[converted_blob_name] = np.array([model_input], dtype=str)
    workspace.RunNet(predict_net)
    return {
        str(blob): workspace.blobs[blob][0] for blob in predict_net.external_outputs
    }


def load_config(filename: str) -> PyTextConfig:
    """
    Load a PyText configuration file from a file path.
    See pytext.config.pytext_config for more info on configs.
    """
    with open(filename) as file:
        config_json = json.loads(file.read())
    if "config" not in config_json:
        return pytext_config_from_json(config_json)
    return pytext_config_from_json(config_json["config"])


def create_predictor(
    config: PyTextConfig, model_file: Optional[str] = None
) -> Predictor:
    """
    Create a simple prediction API from a training config and an exported caffe2
    model file. This model file should be created by calling export on a trained
    model snapshot.
    """
    workspace_id = str(uuid.uuid4())
    workspace.SwitchWorkspace(workspace_id, True)
    predict_net = predictor_exporter.prepare_prediction_net(
        filename=model_file or config.export_caffe2_path, db_type=CAFFE2_DB_TYPE
    )

    supportedInputTensorizers = [
        FloatListTensorizer,
        GazetteerTensorizer,
        TokenTensorizer,
    ]
    new_task = NewTask.from_config(config.task)
    input_tensorizers = {
        name: tensorizer
        for name, tensorizer in new_task.data.tensorizers.items()
        if any(isinstance(tensorizer, t) for t in supportedInputTensorizers)
    }

    return lambda input: _predict(
        workspace_id, predict_net, new_task.model, input_tensorizers, input
    )
