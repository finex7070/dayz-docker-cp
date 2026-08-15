"""Services: everything the panel does besides rendering pages.

These modules own long-lived state (running processes, job history, log
buffers) and are therefore module-level singletons. That is only sound because
the panel runs a single gunicorn worker - see gunicorn.conf.py.
"""
