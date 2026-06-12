# Woddi Harbor v0.3.3

Production document sources now support:

- Markdown: `.md`, `.markdown`
- HTML: `.html`, `.htm`
- Image assets: `.png`

HTML is indexed as visible text without scripts, styles, navigation or SVG markup.
PNG files retain the repository directory structure so HTML and Markdown references
remain available. Images are not interpreted as searchable text.

Reconfigure and synchronize after upgrading:

```bash
.venv/bin/woddi-harbor source configure-docs
.venv/bin/woddi-harbor source sync operation-docs
.venv/bin/woddi-harbor source sync customer-docs
```
