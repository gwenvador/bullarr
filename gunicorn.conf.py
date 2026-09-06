bind = '[::]:5000'
workers = 1
threads = 8
worker_class = 'gthread'
# Import preparation has its own hard subprocess deadline. Do not apply a batch-wide
# Gunicorn timeout to a legitimate multi-file manual import.
timeout = 0
graceful_timeout = 30
keepalive = 5
accesslog = '-'
errorlog = '-'
capture_output = True
