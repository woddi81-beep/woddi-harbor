# Woddi Harbor v0.6.0

OpenStack credentials are now isolated per Harbor user.

- Auth URL, region and timeout remain shared integration settings.
- Every user renews their own project-scoped token in the chat sidebar.
- Tokens, SDK connections and caches are never shared between Harbor users.
- Rotating a token replaces only that user's OpenStack backend and cache.
- The worker starts without an OpenStack token and receives credentials only on
  the authenticated user's internal request.

During the first startup after upgrading, Harbor removes legacy shared
OpenStack token, password and application-credential secrets. Each user must
therefore enter a fresh project-scoped token in the web interface.

`harbor.sh` also compares the installed CLI with the checked-out version and
automatically refreshes a stale virtual environment after `git pull`.

Version and runtime checks:

```bash
./harbor.sh version
./harbor.sh status
./harbor.sh production-check
```
