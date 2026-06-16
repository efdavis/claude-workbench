import {readdir, readFile, writeFile, mkdir, access} from 'node:fs/promises';
import {join, basename, extname, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import os from 'node:os';

const here = dirname(fileURLToPath(import.meta.url));

// Resolve the wiki articles dir the same way docusaurus.config.ts does:
//   CLAUDE_WIKI_DIR (wiki root; articles under <root>/articles), default ./example-wiki.
const expandHome = (p) => p.replace(/^~(?=$|\/)/, os.homedir());
const WIKI_DIR = process.env.CLAUDE_WIKI_DIR
  ? expandHome(process.env.CLAUDE_WIKI_DIR)
  : join(here, '..', 'example-wiki');
const ARTICLES_DIR = join(WIKI_DIR, 'articles');

const OUT_FILE = join(here, '..', 'static', 'backlinks.json');
const ROUTE_BASE = '/wiki';

const WIKILINK_RE = /\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|[^\]]+?)?\]\]/g;

const slugify = (name) => name.trim().toLowerCase().replace(/\s+/g, '-');
const permalinkFor = (slug) =>
  slug === 'index' ? `${ROUTE_BASE}/` : `${ROUTE_BASE}/${slug}`;

const stripFrontMatter = (raw) => raw.replace(/^---[\s\S]*?\n---\n/, '');
const firstParagraph = (body) => {
  const cleaned = body.replace(/^#.*\n+/gm, '').trim();
  const para = cleaned.split(/\n\s*\n/)[0] ?? '';
  return para.replace(/\s+/g, ' ').slice(0, 240);
};

const writeOut = async (payload) => {
  await mkdir(dirname(OUT_FILE), {recursive: true});
  await writeFile(OUT_FILE, JSON.stringify(payload, null, 2));
};

// Boot gracefully when the wiki isn't wired up yet — write empty backlinks so
// the dev server still starts.
try {
  await access(ARTICLES_DIR);
} catch {
  console.warn(
    `[backlinks] articles dir not found: ${ARTICLES_DIR}\n` +
      `[backlinks] set CLAUDE_WIKI_DIR to your wiki root. Writing empty backlinks.json.`,
  );
  await writeOut({links: {}, descriptions: {}, meta: {articles: 0, edges: 0}});
  process.exit(0);
}

const files = (await readdir(ARTICLES_DIR)).filter((f) => extname(f) === '.md');
const slugs = new Set(files.map((f) => slugify(basename(f, '.md'))));

const links = {};
const descriptions = {};

for (const file of files) {
  const fromSlug = slugify(basename(file, '.md'));
  const fromPath = permalinkFor(fromSlug);
  const raw = await readFile(join(ARTICLES_DIR, file), 'utf8');
  const body = stripFrontMatter(raw);

  descriptions[fromPath] = firstParagraph(body);

  const targets = new Set();
  for (const match of body.matchAll(WIKILINK_RE)) {
    const targetSlug = slugify(match[1]);
    if (!slugs.has(targetSlug) || targetSlug === fromSlug) continue;
    targets.add(permalinkFor(targetSlug));
  }
  for (const target of targets) {
    (links[target] ??= []).push(fromPath);
  }
}

for (const target of Object.keys(links)) {
  links[target].sort();
}

const totalBacklinks = Object.values(links).reduce((n, arr) => n + arr.length, 0);
await writeOut({
  links,
  descriptions,
  meta: {articles: files.length, edges: totalBacklinks},
});

console.log(
  `[backlinks] ${files.length} articles → ${Object.keys(links).length} pages with backlinks (${totalBacklinks} edges)`,
);
