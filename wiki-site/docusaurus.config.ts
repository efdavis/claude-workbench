import path from 'path';
import os from 'os';
import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

// Resolve the wiki content directory.
//   CLAUDE_WIKI_DIR  — the wiki ROOT (articles live under <root>/articles).
//                      Shared with the /wiki-* skills; default ~/wiki.
//   Unset            — falls back to the bundled ./example-wiki so the site
//                      boots out of the box with demo content.
const expandHome = (p: string) =>
  p.replace(/^~(?=$|\/)/, os.homedir());

const WIKI_DIR = process.env.CLAUDE_WIKI_DIR
  ? expandHome(process.env.CLAUDE_WIKI_DIR)
  : path.resolve(__dirname, 'example-wiki');
const ARTICLES_DIR = path.join(WIKI_DIR, 'articles');

const config: Config = {
  title: process.env.WIKI_TITLE ?? 'My Wiki',
  tagline: process.env.WIKI_TAGLINE ?? 'Notes, rendered.',
  favicon: 'img/favicon.ico',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Set the production url of your site here. Override via env for deployment.
  url: process.env.WIKI_URL ?? 'https://your-site.example.com',
  // Pathname under which the site is served. For GitHub Pages project sites
  // this is often '/<repo>/'. Override via WIKI_BASE_URL.
  baseUrl: process.env.WIKI_BASE_URL ?? '/',

  // GitHub Pages deployment config — only needed if you `npm run deploy`.
  organizationName: process.env.WIKI_GH_ORG ?? 'your-org',
  projectName: process.env.WIKI_GH_REPO ?? 'wiki-site',

  onBrokenLinks: 'warn',

  stylesheets: [
    {
      href: 'https://fonts.googleapis.com/css2?family=Alfa+Slab+One&family=DM+Sans:wght@400;500;700&display=swap',
      type: 'text/css',
    },
  ],

  markdown: {
    format: 'detect',
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
    // Rewrite Obsidian-style [[wikilinks]] (and [[target|alias]], [[target#heading]])
    // into Docusaurus relative links so the same Markdown renders in both Obsidian
    // and this site.
    preprocessor: ({fileContent}) =>
      fileContent.replace(
        /\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|([^\]]+?))?\]\]/g,
        (_match, target, alias) => {
          const slug = target.trim().toLowerCase().replace(/\s+/g, '-');
          const label = (alias ?? target).trim();
          return `[${label}](./${slug})`;
        },
      ),
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  themes: [
    [
      require.resolve('@easyops-cn/docusaurus-search-local'),
      {
        hashed: true,
        indexBlog: false,
        docsDir: ARTICLES_DIR,
        docsRouteBasePath: '/wiki',
        highlightSearchTermsOnTargetPage: true,
      },
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          path: ARTICLES_DIR,
          routeBasePath: 'wiki',
          sidebarPath: './sidebars.ts',
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/docusaurus-social-card.jpg',
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: false,
    },
    navbar: {
      title: process.env.WIKI_TITLE ?? 'My Wiki',
      logo: {
        alt: 'Wiki Logo',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'tutorialSidebar',
          position: 'left',
          label: 'Articles',
        },
      ],
    },
    footer: {
      style: 'dark',
      copyright: `© ${new Date().getFullYear()} ${process.env.WIKI_AUTHOR ?? ''}`.trim(),
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
