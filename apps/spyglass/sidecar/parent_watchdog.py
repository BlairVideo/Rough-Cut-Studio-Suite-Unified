"""Self-termination on parent death (Section 9-adjacent: an unclean exit of
the Rust host -- force-quit, crash, SIGKILL -- must not leave a
torch/CLIP-loaded Python process running forever).

The Rust side's own cleanup (`EmbedServer`'s `Drop`, `pipeline::run_sidecar`'s
timeout/cancel `child.kill()`) only runs when the host process gets to
execute code on its way out. A SIGKILL'd or crashed host runs no such code,
so its sidecar children become orphans, get reparented, and keep running
indefinitely -- multi-GB of resident torch/CLIP weights, wasted CPU, with no
UI left to reveal they're there.

There's no macOS equivalent of Linux's `prctl(PR_SET_PDEATHSIG)`. The
supported alternative is a `kqueue` watch on the parent's pid with
`EVFILT_PROC`/`NOTE_EXIT`: register it before the parent can disappear, and
the kernel wakes us the moment that specific pid exits, however it exits --
whether this process has since been reparented to launchd or not, since the
watch is keyed by pid, not by "current parent."
"""
import os
import select
import sys
import threading


def exit_if_parent_dies() -> None:
    """Starts a daemon thread that calls `os._exit(1)` the instant the
    process that spawned us exits. Call once, near the top of `main()`,
    before any slow model-loading work begins -- the parent pid must still
    be resolvable via `os.getppid()` at call time.
    """
    parent_pid = os.getppid()

    def _watch() -> None:
        try:
            kq = select.kqueue()
            event = select.kevent(
                parent_pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD,
                fflags=select.KQ_NOTE_EXIT,
            )
            kq.control([event], 0)
        except ProcessLookupError:
            # Parent was already gone by the time we tried to register --
            # possible race at startup wouldn't be a stray process to
            # clean up.
            os._exit(1)  # noqa: SLF001 -- deliberate hard exit, not a normal return path
            return
        except OSError as exc:
            # kqueue isn't POSIX -- only expected to exist on macOS/BSD,
            # which is this app's only supported platform (Section 2), but
            # fail open (log and keep running) rather than crash a
            # legitimate request over a missing watchdog.
            print(f"parent_watchdog: kqueue unavailable, skipping ({exc})", file=sys.stderr, flush=True)
            return

        # Blocks until the parent's NOTE_EXIT event arrives -- no polling,
        # no wasted CPU while everything is healthy.
        kq.control(None, 1, None)
        os._exit(1)  # noqa: SLF001 -- deliberate hard exit; the parent is already gone

    threading.Thread(target=_watch, daemon=True, name="parent-watchdog").start()
