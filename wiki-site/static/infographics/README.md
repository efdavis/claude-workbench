# Infographics

Drop standalone single-file HTML infographics here. Anything in `static/` is
served verbatim, so a file at `static/infographics/foo.html` is reachable at:

```
http://localhost:3200/infographics/foo.html
```

(or `<baseUrl>/infographics/foo.html` once deployed).

Link to them from articles with a normal Markdown link, or embed via an
`<iframe>`. They are not part of the Docusaurus docs pipeline — no frontmatter,
no sidebar entry — so they can be self-contained HTML/JS/CSS.

`example.html` is a minimal placeholder demonstrating the convention; delete it.
