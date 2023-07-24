import torch
from torch._subclasses import FakeTensorMode
from torch.fx.experimental.symbolic_shapes import ShapeEnv, DimDynamic
import torch.nn.functional as F
import torch.fx as fx
from torch._ops import HigherOrderOperator
from torch._C import DispatchKey, DispatchKeySet, _ExcludeDispatchKeyGuard
import torch.utils._pytree as pytree
from torch.fx.experimental.proxy_tensor import (
    disable_proxy_modes_tracing,
    make_fx,
    ProxyTorchDispatchMode,
    track_tensor_tree,
)
from torch.utils._python_dispatch import (
    _get_current_dispatch_mode,
    _pop_mode_temporarily,
)
from torch.fx.experimental.proxy_tensor import make_fx
from torch._inductor.compile_fx import compile_fx_inner

sdpa = HigherOrderOperator("sdpa")

@sdpa.py_impl(DispatchKey.CompositeExplicitAutograd)
def sdpa_dense(q, k, v, score_mod):
    out = F.scaled_dot_product_attention(q, k, v, scale=1).contiguous()
    return out

@sdpa.py_impl(DispatchKey.Autograd)
def sdpa_autograd(*args, **kwargs):
    with _ExcludeDispatchKeyGuard(DispatchKeySet(DispatchKey.AutogradCPU)):
        return sdpa(*args, **kwargs)

def trace_sdpa(proxy_mode, q, k, v, score_mod):
    if score_mod is None:
        with proxy_mode:
            return F.scaled_dot_product_attention(q, k, v)

    with disable_proxy_modes_tracing():
        example_out = F.scaled_dot_product_attention(q, k, v)
    example_vals = [torch.zeros((), dtype=q.dtype)] + [torch.zeros((), dtype=torch.int) for _ in range(4)]
    score_graph = make_fx(score_mod)(*example_vals)
    proxy_mode.tracer.root.register_module("sdpa_score", score_graph)
    node_args = (q, k, v, score_graph)
    proxy_args = pytree.tree_map(proxy_mode.tracer.unwrap_proxy, node_args)
    out_proxy = proxy_mode.tracer.create_proxy('call_function', sdpa, proxy_args, {}, name="sdpa_impl")
    return track_tensor_tree(example_out, out_proxy, constant=None, tracer=proxy_mode.tracer)

@sdpa.py_impl(ProxyTorchDispatchMode)
def sdpa_proxy_torch_dispatch_mode(q, k, v, score_mod):
    mode = _get_current_dispatch_mode()
    assert (mode is not None), "Mode should always be enabled for python fallback key"
    with _pop_mode_temporarily() as mode:
        if mode.enable_tracing:
            return trace_sdpa(mode, q, k, v, score_mod)
        else:
            return sdpa(q, k, v, score_mod)

@sdpa.py_impl(FakeTensorMode)
def sdpa_fake_tensor_mode(*args, **kwargs):
    return sdpa_dense(*args, **kwargs)

sdpa.fallthrough(DispatchKey.PythonDispatcher)
sdpa.fallthrough(DispatchKey.PythonTLSSnapshot)
sdpa.fallthrough(DispatchKey.ADInplaceOrView)
sdpa.fallthrough(DispatchKey.BackendSelect)
sdpa.fallthrough(DispatchKey.AutocastCPU)
sdpa.fallthrough(DispatchKey.AutocastCPU)

Z = 1
H = 4
N_CTX = 512
D_HEAD = 64
dtype = torch.float16
torch.manual_seed(0)
q = torch.randn((Z, H, N_CTX, D_HEAD), dtype=dtype, device="cuda")
k = torch.randn((Z, H, N_CTX, D_HEAD), dtype=dtype, device="cuda")
v = torch.randn((Z, H, N_CTX, D_HEAD), dtype=dtype, device="cuda")
# v[0][0] *= 2
# v[0][1:] = 0

# @torch.compile
def foo(q, k, v):
    return (sdpa(q, k, v, lambda score, b, h, m, n: score + (m - n)),)

# foo(q, k, v)
# exit(0)
fake_mode = FakeTensorMode()
with fake_mode:
    q_, k_, v_ = [fake_mode.from_tensor(t) for t in (q, k, v)]
    out_graph = make_fx(foo)(q_, k_, v_)
    out_graph.print_readable()
    out_graph = compile_fx_inner(out_graph, (q_, k_, v_))
o1 = out_graph([q, k, v])
print(o1)
print(foo(q, k, v))
breakpoint()