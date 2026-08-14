# Memory Guard

Memory Guard runs before Ollama install verification, explicit `omm verify`, and
benchmark operations; it never blocks a download. The default `ask` policy offers
to release only resident models that
the OMM registry proves are OMM-managed. `block` refuses an unsafe load, and
`observe` warns but never unloads anything.

The planner recalculates live RAM/VRAM capacity using the existing conservative
memory budget. Unified-memory systems count the shared pool once; dedicated RAM
and VRAM requirements can be checked separately. After consent, cleanup uses the
owning runtime API, proves the model is no longer resident, and rescans memory
before allowing the new load.

Long install and contribution benchmarks require several consecutive low-memory
samples for a configured duration before action. Only the model operation started
by OMM may be cancelled; if cancellation cannot be confirmed, the operation is
reported as failed.
There is no process-kill, `sudo`, or arbitrary application cleanup path.

Configure it with:

```text
omm setting memory-guard --policy ask
omm setting memory-guard --poll-seconds 1 --low-memory-seconds 3
```

The new config keys are optional on read and invalid stored values safely fall
back to `ask`, 1 second polling, and 3 seconds of sustained pressure. Rollback is
therefore compatible with older clients, which ignore these keys.
