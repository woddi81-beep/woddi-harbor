# Woddi Harbor v0.3.12

OpenStack project fields are now optional.

- Leave project and project domain empty for an already project-scoped token.
- Enter a project for an unscoped token; the domain defaults to `Default`.

After upgrading, save the OpenStack configuration once with both project fields
empty and restart the OpenStack module.
