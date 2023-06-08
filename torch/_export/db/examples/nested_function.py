import torch

from torch._export.db.case import export_case


@export_case(
    example_inputs=(torch.ones(3, 2), torch.ones(2)),
    tags={"python.closure"},
)
def nested_function(a, b):
    """
    Nested functions are traced through. Side effects on global captures
    are not supported though.
    """
    x = a + b
    z = a - b

    def closure(y):
        nonlocal x
        x += 1
        return x * y + z

    return closure(x)
