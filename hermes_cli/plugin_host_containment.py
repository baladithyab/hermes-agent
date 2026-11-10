"""Contain an ambiguous timed-out RPC without invoking plugin cleanup or replay."""


def contain_deadline(host, channel):
    # Match the transport object, not a PID or the current generation guessed
    # after the deadline. A late failure must never kill a replacement process.
    with host._lock:
        if host._channel is not channel:
            return
        host._deadline_failed = True
        host._stopping = True
        process = host._proc
        host._generation += 1  # immediately fail accepted-policy revalidation
    # Revoke capabilities before process cleanup; late callback results cannot
    # publish authority even if the callback itself cannot be cancelled.
    host._request_operations.shutdown()
    # Do not send shutdown: the event loop may be blocked or ignore cancellation.
    # Kill only this direct Popen handle, never its group/tree: independently
    # owned native services must survive an API transport failure.
    if process is not None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
    channel.close('plugin RPC deadline; outcome unknown; no automatic replay')
