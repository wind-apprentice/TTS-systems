import argparse
import os

import comet_ml
import pytorch_lightning as pl
import torch
import yaml
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from pytorch_lightning.profiler import AdvancedProfiler
from Parsers.parser import DataParser

from tts.datamodules import get_datamodule
from tts.systems import get_system
import Define

quiet = False
if quiet:
    # NOTSET/DEBUG/INFO/WARNING/ERROR/CRITICAL
    os.environ["COMET_LOGGING_CONSOLE"] = "ERROR"
    import warnings
    warnings.filterwarnings("ignore")
    import logging
    # configure logging at the root level of lightning
    logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)
if Define.CUDA_LAUNCH_BLOCKING:
    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'


TRAINER_CONFIG = {
    "gpus": -1 if torch.cuda.is_available() else None,
    "strategy": "ddp" if torch.cuda.is_available() else None,
    "auto_select_gpus": True,
    "deterministic": True,
    "process_position": 1,
    "profiler": 'simple',
}

if Define.DEBUG:
    TRAINER_CONFIG.update({
        "limit_train_batches": 50,  # Useful for debugging
        "limit_val_batches": 50,  # Useful for debugging
    })


def main(args, configs):
    data_configs, model_config, train_config, algorithm_config = configs

    # register parsers and simply average stats over different datasets.
    import json
    keys = []
    for data_config in data_configs:
        Define.DATAPARSERS[data_config["name"]] = DataParser(data_config["data_dir"])
        data_parser = Define.DATAPARSERS[data_config["name"]]
        with open(data_parser.stats_path) as f:
            stats = json.load(f)
            stats = stats["pitch"] + stats["energy"]
            Define.ALLSTATS[data_config["name"]] = stats
            keys.append(data_config["name"])

    Define.ALLSTATS["global"] = Define.merge_stats(Define.ALLSTATS, keys)
    if Define.DEBUG:
        print("Initialize data parsers and build normalization stats, done.")
        input()

    # Checkpoint for resume training or testing
    pretrain_ckpt_file = args.pretrain_path

    # Configure pytorch lightning trainer
    trainer_training_config = {
        'max_steps': train_config["step"]["total_step"],
        'log_every_n_steps': train_config["step"]["log_step"],
        'gradient_clip_val': train_config["optimizer"]["grad_clip_thresh"],
        'accumulate_grad_batches': train_config["optimizer"]["grad_acc_step"],
        'resume_from_checkpoint': pretrain_ckpt_file,
    }

    # Init logger
    ckpt_dir = f"output/{args.exp_name}/ckpt"
    result_dir = f"output/{args.exp_name}/result"
    log_dir = f"output/{args.exp_name}/log"
    train_config["path"] = {
        "ckpt_path": ckpt_dir,
        "log_path": log_dir,
        "result_path": result_dir,
    }
    for p in train_config["path"].values():
        os.makedirs(p, exist_ok=True)

    loggers = None
    if args.stage in ["train"]:
        if Define.LOGGER == "tb":
            from pytorch_lightning.loggers import TensorBoardLogger
            tb_logger = TensorBoardLogger(log_dir, name="tb")
            loggers = [tb_logger]
        elif Define.LOGGER == "comet":
            pass

    datamodule = get_datamodule(algorithm_config["type"])(
        data_configs, train_config, algorithm_config, log_dir, result_dir
    )

    if Define.DEBUG:
        print("All components except system module are prepared.")
        input()
    
    if args.stage == 'train':
        # Get model
        system = get_system(algorithm_config["type"])
        if pretrain_ckpt_file is None:
            model = system(
                model_config, train_config, algorithm_config,
                log_dir, result_dir
            )
        else:
            model = system.load_from_checkpoint(
                pretrain_ckpt_file, 
                model_config=model_config, train_config=train_config, algorithm_config=algorithm_config,
                log_dir=log_dir, result_dir=result_dir
            )

        # Train
        if Define.DEBUG:
            print("System module prepared.")
            input()
            print("Start Training!")

        if loggers is not None:
            trainer = pl.Trainer(logger=loggers, **TRAINER_CONFIG, **trainer_training_config)
        else:
            trainer = pl.Trainer(**TRAINER_CONFIG, **trainer_training_config)
        pl.seed_everything(43, True)
        trainer.fit(model, datamodule=datamodule)

    elif args.stage == 'test' or args.stage == 'predict':
        # Get model
        assert pretrain_ckpt_file is not None
        system = get_system(algorithm_config["type"])
        model = system.load_from_checkpoint(pretrain_ckpt_file)
        # Test
        trainer = pl.Trainer(**TRAINER_CONFIG)
        trainer.test(model, datamodule=datamodule)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-d", "--data_config", type=str, nargs='+', help="path to data config directory",
        default=['data_config/LJSpeech-1.1'],
    )
    parser.add_argument(
        "-m", "--model_config", type=str, help="path to model.yaml",
        default='config/FastSpeech2/model/base.yaml',
    )
    parser.add_argument(
        "-t", "--train_config", type=str, help="path to train.yaml",
        default='config/FastSpeech2/train/baseline.yaml',
    )
    parser.add_argument(
        "-a", "--algorithm_config", type=str, help="path to algorithm.yaml",
        default='config/FastSpeech2/algorithm/baseline.yaml',
    )
    parser.add_argument(
        "-n", "--exp_name", type=str, help="experiment name, default is algorithm's name",
        default=None,
    )
    parser.add_argument(
        "-pre", "--pretrain_path", type=str, help="pretrained model path",
        default=None,
    )
    parser.add_argument(
        "-s", "--stage", type=str, help="stage (train/test)",
        default="train",
    )
    args = parser.parse_args()

    # Read config
    from Objects.config import DataConfigReader
    config_reader = DataConfigReader()
    data_configs = [config_reader.read(path) for path in args.data_config]
    
    model_config = yaml.load(
        open(args.model_config, "r"), Loader=yaml.FullLoader
    )
    train_config = yaml.load(
        open(args.train_config, "r"), Loader=yaml.FullLoader
    )
    algorithm_config = yaml.load(
        open(args.algorithm_config, "r"), Loader=yaml.FullLoader
    )

    if args.exp_name is None:
        args.exp_name = algorithm_config["name"]
    
    configs = (data_configs, model_config, train_config, algorithm_config)

    main(args, configs)