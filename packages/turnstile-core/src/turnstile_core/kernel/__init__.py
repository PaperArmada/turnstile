"""The kernel: pure transition semantics.

Given a definition, an instance snapshot, and a command (transition,
signal, skip), the kernel decides what should happen — which gates to
run, which state to enter, what children to spawn — without performing
any I/O. The runtime executes those decisions.
"""
