# Copyright Lightning AI. Licensed under the Apache License 2.0, see LICENSE file.

import math
import sys
import time
from pathlib import Path
from typing import Optional, Tuple, Union

import lightning as L
import numpy as np
import torch
from lightning.fabric.loggers import CSVLogger
from lightning.fabric.strategies import FSDPStrategy
from lightning.fabric.utilities import ThroughputMonitor, measure_flops
from torch.utils.data import DataLoader, IterableDataset

# support running without installing as a package
wd = Path(__file__).parent.parent.resolve()
sys.path.append(str(wd))

from lit_gpt import Config
from lit_gpt.args import EvalArgs, IOArgs, TrainArgs
from lit_gpt.model import GPT, Block
from lit_gpt.utils import CLI, chunked_cross_entropy, estimate_flops, get_default_supported_precision, num_parameters


def setup(
    model_name: str = "pythia-70m",
    precision: Optional[str] = None,
    resume: Union[bool, Path] = False,
    devices: int = 1,
    io: IOArgs = IOArgs(train_data_dir=Path("data/openwebtext"), val_data_dir=None, out_dir=Path("out/openwebtext")),
    train: TrainArgs = TrainArgs(
        save_interval=1000,
        log_interval=1,
        global_batch_size=125,
        micro_batch_size=5,
        lr_warmup_steps=100,
        epochs=1,
        epoch_size=600000,
        learning_rate=6e-4,
        weight_decay=1e-1,
        beta1=0.9,
        beta2=0.95,
        max_norm=1.0,
        min_lr=6e-5,
    ),
    eval: EvalArgs = EvalArgs(interval=1000, max_iters=100),
) -> None:
    print(locals())
    precision = precision or get_default_supported_precision(training=True)

    if devices > 1:
        strategy = FSDPStrategy(
            auto_wrap_policy={Block},
            activation_checkpointing_policy={Block},
            state_dict_type="full",
            limit_all_gathers=True,
            cpu_offload=False,
        )
    else:
        strategy = "auto"

    logger = CSVLogger(io.out_dir.parent, io.out_dir.name, flush_logs_every_n_steps=train.log_interval)
    fabric = L.Fabric(devices=devices, strategy=strategy, precision=precision, loggers=logger)

    fabric.launch(main, devices, resume, Config.from_name(name=model_name), io, train, eval)


def main(
    fabric: L.Fabric,
    devices: int,
    resume: Union[bool, Path],
    config: Config,
    io: IOArgs,
    train: TrainArgs,
    eval: EvalArgs,
) -> None:
    validate_args(io, train, eval)

    if fabric.global_rank == 0:
        io.out_dir.mkdir(parents=True, exist_ok=True)

    fabric.seed_everything(1337, workers=True)  # same seed for every process to init model (FSDP)

    fabric.print(f"Loading model with {config.__dict__}")
    t0 = time.perf_counter()
    with fabric.init_module(empty_init=(fabric.world_size > 1)):
        model = GPT(config)
    model.apply(model._init_weights)

    fabric.print(f"Time to instantiate model: {time.perf_counter() - t0:.02f} seconds.")
    fabric.print(f"Total parameters {num_parameters(model):,}")

    model = fabric.setup(model)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=train.learning_rate,
        weight_decay=train.weight_decay,
        betas=(train.beta1, train.beta2),
        foreach=False,
    )
    optimizer = fabric.setup_optimizers(optimizer)

    train_data, val_data = load_datasets(io, max_seq_length=model.max_seq_length)
    train_dataloader = DataLoader(train_data, batch_size=train.micro_batch_size, num_workers=2)
    val_dataloader = DataLoader(val_data, batch_size=train.micro_batch_size, num_workers=2)
    train_dataloader, val_dataloader = fabric.setup_dataloaders(train_dataloader, val_dataloader)

    state = {"model": model, "optimizer": optimizer, "iter_num": 0, "step_count": 0}

    if resume is True:
        resume = max(io.out_dir.glob("*.pth"), key=lambda p: int(p.name.split("-")[1]))
    if resume:
        fabric.print(f"Resuming training from {resume}")
        fabric.load(resume, state)

    train_time = time.perf_counter()
    fit(fabric, devices, state, train_dataloader, val_dataloader, io, train, eval)
    fabric.print(f"Training time: {(time.perf_counter()-train_time):.2f}s")
    if fabric.device.type == "cuda":
        fabric.print(f"Memory used: {torch.cuda.max_memory_allocated() / 1e9:.02f} GB")


def fit(
    fabric: L.Fabric,
    devices: int,
    state: dict,
    train_dataloader: DataLoader,
    val_dataloader: DataLoader,
    io: IOArgs,
    train: TrainArgs,
    eval: EvalArgs,
) -> None:
    model = state["model"]
    optimizer = state["optimizer"]

    validate(fabric, model, val_dataloader, max_iters=2)  # sanity check

    with torch.device("meta"):
        meta_model = GPT(model.config)
        # "estimated" is not as precise as "measured". Estimated is optimistic but widely used in the wild.
        # When comparing MFU or FLOP numbers with other projects that use estimated FLOPs,
        # consider passing `flops_per_batch=estimated_flops` instead
        estimated_flops = estimate_flops(meta_model, training=True) * train.micro_batch_size
        fabric.print(f"Estimated TFLOPs: {estimated_flops * fabric.world_size / 1e12:.2f}")
        x = torch.randint(0, 1, (train.micro_batch_size, model.max_seq_length))
        forward_fn = lambda: meta_model(x)
        loss_fn = lambda y: chunked_cross_entropy(y, x, chunk_size=0)
        measured_flops = measure_flops(meta_model, forward_fn, loss_fn)
        fabric.print(f"Measured TFLOPs: {measured_flops * fabric.world_size / 1e12:.2f}")
        del meta_model, x

    throughput = ThroughputMonitor(fabric, window_size=50)
    total_t0 = time.perf_counter()

    train_iter = iter(train_dataloader)

    lr_warmup_iters = train.lr_warmup_steps * train.gradient_accumulation_iters(devices)
    for state["iter_num"] in range(state["iter_num"], train.max_iters(devices)):
        # determine and set the learning rate for this iteration
        lr = get_lr(
            train.learning_rate, state["iter_num"], lr_warmup_iters, train.max_iters(devices), min_lr=train.min_lr
        )
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr

        iter_num = state["iter_num"] + 1
        iter_t0 = time.perf_counter()

        input_ids, targets = next(train_iter)

        is_accumulating = iter_num % train.gradient_accumulation_iters(devices) != 0
        with fabric.no_backward_sync(model, enabled=is_accumulating):
            logits = model(input_ids)
            loss = chunked_cross_entropy(logits, targets, chunk_size=0)
            fabric.backward(loss / train.gradient_accumulation_iters(devices))

        if not is_accumulating:
            fabric.clip_gradients(model, optimizer, max_norm=train.max_norm)
            optimizer.step()
            optimizer.zero_grad()
            state["step_count"] += 1

        if iter_num % train.log_interval == 0:
            loss_item = loss.item()  # expensive device-to-host synchronization
            t1 = time.perf_counter()
            throughput.update(
                time=t1 - total_t0,
                batches=iter_num,
                samples=iter_num * train.micro_batch_size,
                lengths=iter_num * train.micro_batch_size * model.max_seq_length,
                flops=measured_flops * train.log_interval,
            )
            throughput.compute_and_log(step=iter_num)
            fabric.print(
                f"iter {iter_num} step {state['step_count']}: loss {loss_item:.4f}, iter time:"
                f" {(t1 - iter_t0) * 1000:.2f}ms{' (optimizer.step)' if not is_accumulating else ''}"
            )

        if not is_accumulating and state["step_count"] % eval.interval == 0:
            t0 = time.perf_counter()
            val_loss = validate(fabric, model, val_dataloader, max_iters=eval.max_iters)
            t1 = time.perf_counter() - t0
            fabric.print(f"step {iter_num}: val loss {val_loss.item():.4f}, val time: {t1 * 1000:.2f}ms")
            fabric.barrier()
        if not is_accumulating and state["step_count"] % train.save_interval == 0:
            checkpoint_path = io.out_dir / f"iter-{iter_num:06d}-ckpt.pth"
            fabric.print(f"Saving checkpoint to {str(checkpoint_path)!r}")
            fabric.save(checkpoint_path, state)


# FSDP has issues with `inference_mode`
@torch.no_grad()
def validate(fabric: L.Fabric, model: torch.nn.Module, val_dataloader: DataLoader, max_iters: int) -> torch.Tensor:
    fabric.print("Validating ...")
    model.eval()
    val_iter = iter(val_dataloader)

    losses = torch.zeros(max_iters, device=fabric.device)
    for k in range(max_iters):
        input_ids, targets = next(val_iter)
        logits = model(input_ids)
        losses[k] = chunked_cross_entropy(logits, targets, chunk_size=0)
    out = losses.mean()

    model.train()
    return out


def load_datasets(io: IOArgs, max_seq_length: int) -> Tuple["Dataset", "Dataset"]:
    train_data = Dataset(io.train_data_dir / "train.bin", max_seq_length)
    val_data = Dataset(io.val_data_dir / "val.bin", max_seq_length)
    return train_data, val_data


class Dataset(IterableDataset):
    def __init__(self, data_file: Path, max_seq_length: int):
        super().__init__()
        self.data_file = data_file
        self.max_seq_length = max_seq_length

    def __iter__(self):
        data = np.memmap(self.data_file, dtype=np.uint16, mode="r")
        while True:
            i = torch.randint(len(data) - self.max_seq_length, (1,)).item()
            x = torch.from_numpy((data[i : i + self.max_seq_length]).astype(np.int64))
            y = torch.from_numpy((data[i + 1 : i + 1 + self.max_seq_length]).astype(np.int64))
            yield x, y


# learning rate decay scheduler (cosine with linear warmup)
def get_lr(learning_rate: float, it: int, warmup_iters: int, max_iters: int, min_lr: float) -> float:
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * it / warmup_iters
    # 2) if it > max_iters, return min learning rate
    if it > max_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (max_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)


def validate_args(io: IOArgs, train: TrainArgs, eval: EvalArgs) -> None:
    issues = []
    unsupported = [(io, ["checkpoint_dir"]), (train, ["max_tokens"]), (eval, ["max_new_tokens"])]
    for args, names in unsupported:
        for name in names:
            if getattr(args, name) is not None:
                issues.append(f"{__file__} doesn't support the {name!r} argument. This is set in {args}")
    required = [(io, ["train_data_dir", "val_data_dir"]), (train, ["epoch_size", "epochs", "max_norm"])]
    for args, names in required:
        for name in names:
            if getattr(args, name) is None:
                issues.append(f"{__file__} requires the {name!r} argument. This is set in {args}")
    if issues:
        raise ValueError("\n".join(issues))


if __name__ == "__main__":
    torch.set_float32_matmul_precision("high")

    CLI(setup)
