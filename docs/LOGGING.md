# Application Logging

The application writes logs to both stdout and `logs/app.log`. The file rotates
at midnight and keeps the latest 14 daily files. Runtime log level is controlled
by `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, or `ERROR`).

Each HTTP request has a correlation ID. Clients may send an `X-Request-ID`
containing only letters, numbers, `.`, `_`, `:`, or `-`; otherwise the server
generates one. The same value is returned in the response header and appears in
all application logs produced while handling that request.

Request boundary records use these searchable event names:

- `request_started`: method, path, and client address
- `request_completed`: method, path, status, duration in milliseconds, and response size
- `request_failed`: method, path, status `500`, duration, client address, and traceback

Request bodies, authorization headers, cookies, passwords, and tokens are not
written by the request middleware. To investigate one request, search
`logs/app.log` for its `request_id`, for example:

```text
request_id=health-check-20260801
```

