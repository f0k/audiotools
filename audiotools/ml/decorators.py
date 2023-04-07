import os
import time
from collections import defaultdict
from functools import wraps

import torch
import torch.distributed as dist
from rich import box
from rich.console import Console
from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import BarColumn
from rich.progress import Progress
from rich.progress import SpinnerColumn
from rich.progress import TimeElapsedColumn
from rich.progress import TimeRemainingColumn
from rich.rule import Rule
from rich.table import Table
from torch.utils.tensorboard import SummaryWriter


# This is here so that the history can be pickled.
def default_list():
    return []


class Mean:
    def __init__(self):
        self.reset()

    def __call__(self):
        mean = self.total / self.count
        return mean

    def reset(self):
        self.count = 0
        self.total = 0

    def update(self, val):
        self.count += 1
        self.total += val


def when(condition):
    """Runs a function only when the condition is met. The condition is
    a function that is run.

    Parameters
    ----------
    condition : Callable
        Function to run to check whether or not to run the decorated
        function.

    Example
    -------
    Checkpoint only runs every 100 iterations, and only if the
    local rank is 0.

    >>> i = 0
    >>> rank = 0
    >>>
    >>> @when(lambda: i % 100 == 0 and rank == 0)
    >>> def checkpoint():
    >>>     print("Saving to /runs/exp1")
    >>>
    >>> for i in range(1000):
    >>>     checkpoint()

    """

    def decorator(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            if condition():
                return fn(*args, **kwargs)

        return decorated

    return decorator


def timer(prefix: str = "time"):
    """Adds execution time to the output dictionary of the decorated
    function. The function decorated by this must output a dictionary.
    The key added will follow the form "[prefix]/[name_of_function]"

    Parameters
    ----------
    prefix : str, optional
        The key added will follow the form "[prefix]/[name_of_function]",
        by default "time".
    """

    def decorator(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            s = time.perf_counter()
            output = fn(*args, **kwargs)
            assert isinstance(output, dict)
            e = time.perf_counter()
            output[f"{prefix}/{fn.__name__}"] = e - s
            return output

        return decorated

    return decorator


class Tracker:
    def __init__(
        self,
        writer: SummaryWriter = None,
        log_file: str = None,
        rank: int = 0,
        console_width: int = 87,
        step: int = 0,
    ):
        self.metrics = {}
        self.history = {}
        self.writer = writer
        self.rank = rank
        self.step = step

        # Create progress bars etc.
        self.tasks = {}
        self.pbar = Progress(
            SpinnerColumn(),
            "[progress.description]{task.description}",
            "{task.completed}/{task.total}",
            BarColumn(),
            TimeElapsedColumn(),
            "/",
            TimeRemainingColumn(),
        )
        self.consoles = [Console(width=console_width)]
        self.live = Live(console=self.consoles[0], refresh_per_second=10)
        if log_file is not None:
            self.consoles.append(Console(width=console_width, file=open(log_file, "a")))

    def print(self, msg):
        if self.rank == 0:
            for c in self.consoles:
                c.log(msg)

    def update(self, label, fn_name):
        if self.rank == 0:
            self.pbar.advance(self.tasks[label]["pbar"])

            # Create table
            table = Table(title=label, expand=True, box=box.MINIMAL)
            table.add_column("key", style="cyan")
            table.add_column("value", style="bright_blue")
            table.add_column("mean", style="bright_green")

            keys = self.metrics[label]["value"].keys()
            for k in keys:
                value = self.metrics[label]["value"][k]
                mean = self.metrics[label]["mean"][k]()
                table.add_row(k, f"{value:10.6f}", f"{mean:10.6f}")

            self.tasks[label]["table"] = table
            tables = [t["table"] for t in self.tasks.values()]
            group = Group(*tables, self.pbar)
            self.live.update(
                Group(
                    Padding("", (0, 0)),
                    Rule(f"[italic]{fn_name}()", style="white"),
                    Padding("", (0, 0)),
                    Panel.fit(
                        group, padding=(0, 5), title="[b]Progress", border_style="blue"
                    ),
                )
            )

    def done(self, label: str, title: str):
        for label in self.metrics:
            for v in self.metrics[label]["mean"].values():
                v.reset()

        if self.rank == 0:
            self.pbar.reset(self.tasks[label]["pbar"])
            self.print(
                Group(
                    Markdown(f"# {title}"), *[t["table"] for t in self.tasks.values()]
                )
            )

    def track(
        self,
        label: str,
        length: int,
        completed: int = 0,
        op: dist.ReduceOp = dist.ReduceOp.AVG,
    ):
        self.tasks[label] = {
            "pbar": self.pbar.add_task(
                f"[white]Iteration ({label})", total=length, completed=completed
            ),
            "table": Table(),
        }
        self.metrics[label] = {
            "value": defaultdict(),
            "mean": defaultdict(lambda: Mean()),
        }
        ddp_active = "LOCAL_RANK" in os.environ

        def decorator(fn):
            @wraps(fn)
            def decorated(*args, **kwargs):
                output = fn(*args, **kwargs)
                assert isinstance(output, dict)
                # Collect across all DDP processes
                for k, v in output.items():
                    if not torch.is_tensor(v):
                        continue
                    if ddp_active:  # pragma: no cover
                        dist.all_reduce(v, op=op)
                    output[k] = v.detach().item()

                # Save the outputs to tracker
                for k, v in output.items():
                    self.metrics[label]["value"][k] = v
                    # Update the running mean
                    self.metrics[label]["mean"][k].update(v)

                self.update(label, fn.__name__)
                return output

            return decorated

        return decorator

    def log(self, label: str, value_type: str = "value", history: bool = True):
        assert value_type in ["mean", "value"]
        if history:
            if label not in self.history:
                self.history[label] = defaultdict(default_list)

        def decorator(fn):
            @wraps(fn)
            def decorated(*args, **kwargs):
                output = fn(*args, **kwargs)
                if self.rank == 0:
                    nonlocal value_type, label
                    metrics = self.metrics[label][value_type]
                    for k, v in metrics.items():
                        v = v() if isinstance(v, Mean) else v
                        if self.writer is not None:
                            self.writer.add_scalar(f"{k}/{label}", v, self.step)
                        if label in self.history:
                            self.history[label][k].append(v)

                    if label in self.history:
                        self.history[label]["step"].append(self.step)

                return output

            return decorated

        return decorator

    def is_best(self, label, key):
        return self.history[label][key][-1] == min(self.history[label][key])

    def state_dict(self):
        return {"history": self.history, "step": self.step}

    def load_state_dict(self, state_dict):
        self.history = state_dict["history"]
        self.step = state_dict["step"]
        return self
