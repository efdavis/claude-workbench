import {useEffect, useState, type ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import useBaseUrl from '@docusaurus/useBaseUrl';
import Layout from '@theme/Layout';

import styles from './index.module.css';

/**
 * Apatosaurus silhouette (CC0) — Wikimedia Commons:
 *   https://commons.wikimedia.org/wiki/File:Apatosaurus_Silhouette.svg
 * Stegosaurus silhouette (CC0) — Wikimedia Commons:
 *   https://commons.wikimedia.org/wiki/File:Stegosaurus_silhouette.svg
 * Pteranodon longiceps (CC BY 4.0) by Matt Dempsey — PhyloPic:
 *   https://www.phylopic.org/images/071babce-127c-4e5d-8472-62e17ad1e7e1
 */
function Dino({
  src,
  x,
  y,
  width,
  height,
  flip = false,
}: {
  src: string;
  x: number;
  y: number;
  width: number;
  height: number;
  flip?: boolean;
}) {
  const transform = flip
    ? `translate(${x + width},${y}) scale(-1,1)`
    : `translate(${x},${y})`;
  return (
    <g transform={transform}>
      <image href={src} width={width} height={height} />
    </g>
  );
}

function Pines() {
  const trees: Array<{x: number; y: number; h: number}> = [
    {x: 60, y: 720, h: 110},
    {x: 130, y: 700, h: 140},
    {x: 210, y: 730, h: 95},
    {x: 280, y: 705, h: 130},
    {x: 360, y: 735, h: 90},
    {x: 1240, y: 730, h: 100},
    {x: 1320, y: 700, h: 145},
    {x: 1400, y: 720, h: 115},
    {x: 1470, y: 740, h: 90},
    {x: 1540, y: 710, h: 125},
  ];
  return (
    <g fill="#0e1808">
      {trees.map(({x, y, h}, i) => (
        <g key={i}>
          <rect x={x - 5} y={y + h - 12} width="10" height="18" />
          <polygon
            points={`${x},${y} ${x - h * 0.32},${y + h} ${x + h * 0.32},${y + h}`}
          />
          <polygon
            points={`${x},${y + h * 0.22} ${x - h * 0.27},${y + h * 0.82} ${
              x + h * 0.27
            },${y + h * 0.82}`}
          />
          <polygon
            points={`${x},${y + h * 0.5} ${x - h * 0.21},${y + h * 0.95} ${
              x + h * 0.21
            },${y + h * 0.95}`}
          />
        </g>
      ))}
    </g>
  );
}

function PosterScene() {
  const img = (name: string) => useBaseUrl(`/img/${name}`);
  return (
    <svg
      className={styles.scene}
      viewBox="0 0 1600 900"
      preserveAspectRatio="xMidYMax slice"
      aria-hidden="true"
    >
      {/* dusk sky */}
      <rect width="1600" height="900" fill="#1e2a3a" />

      {/* atmospheric band near horizon */}
      <rect y="380" width="1600" height="80" fill="#2e3d4f" />
      <rect y="460" width="1600" height="40" fill="#3d4a5a" />

      {/* sun */}
      <circle cx="800" cy="430" r="190" fill="#c25e28" />
      {/* sun reflected band on far ridge */}
      <rect x="610" y="470" width="380" height="14" fill="#d4a574" />

      {/* pteranodons soaring in the sky */}
      <Dino src={img('pteranodon.svg')} x={210} y={140} width={260} height={117} />
      <Dino
        src={img('pteranodon.svg')}
        x={1140}
        y={110}
        width={180}
        height={81}
        flip
      />
      <Dino src={img('pteranodon.svg')} x={930} y={240} width={110} height={49} />

      {/* far mountains (slate-green silhouette) */}
      <path
        d="M0,520 L90,440 L180,490 L270,420 L380,500 L470,440 L560,490
           L660,430 L760,500 L860,440 L960,490 L1050,420 L1160,490
           L1250,430 L1360,500 L1460,440 L1560,490 L1600,470
           L1600,900 L0,900 Z"
        fill="#3a4a3a"
      />

      {/* mid mountains (pine) */}
      <path
        d="M0,640 L120,550 L220,610 L340,500 L460,590 L580,520 L720,610
           L860,530 L1000,610 L1140,510 L1280,600 L1420,520 L1540,600 L1600,560
           L1600,900 L0,900 Z"
        fill="#2b3a1f"
      />

      {/* distant apatosaurus on the mid-mountain ridge */}
      <Dino
        src={img('apatosaurus.svg')}
        x={300}
        y={530}
        width={180}
        height={46}
      />

      {/* foreground meadow ridge */}
      <path
        d="M0,740 L160,700 L340,740 L520,710 L700,740 L880,710 L1080,745
           L1260,705 L1440,745 L1600,720 L1600,900 L0,900 Z"
        fill="#5a7e3b"
      />

      {/* big apatosaurus walking the meadow */}
      <Dino
        src={img('apatosaurus.svg')}
        x={420}
        y={620}
        width={420}
        height={107}
      />
      {/* stegosaurus on the right, facing left */}
      <Dino
        src={img('stegosaurus.svg')}
        x={1020}
        y={650}
        width={260}
        height={150}
        flip
      />

      <Pines />

      {/* ground band */}
      <rect y="860" width="1600" height="40" fill="#0e1808" />
    </svg>
  );
}

// Live counts come from the generated backlinks.json (written by
// scripts/generate-backlinks.mjs on prestart/prebuild). Fetched client-side so
// the static page never embeds stale numbers.
function useWikiStats() {
  const url = useBaseUrl('/backlinks.json');
  const [stats, setStats] = useState<{articles: number; edges: number} | null>(
    null,
  );
  useEffect(() => {
    let live = true;
    fetch(url)
      .then((r) => r.json())
      .then((d) => {
        if (!live) return;
        const articles =
          d?.meta?.articles ?? Object.keys(d?.descriptions ?? {}).length;
        const edges =
          d?.meta?.edges ??
          Object.values(d?.links ?? {}).reduce(
            (n: number, a: unknown[]) => n + (a?.length ?? 0),
            0,
          );
        setStats({articles, edges});
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [url]);
  return stats;
}

function PosterHero() {
  const {siteConfig} = useDocusaurusContext();
  const stats = useWikiStats();
  return (
    <section className={styles.poster}>
      <PosterScene />

      <div className={styles.posterFrame}>
        <div className={styles.eyebrow}>
          <span>FIELD NOTES</span>
        </div>
        <h1 className={styles.title}>{siteConfig.title}</h1>
        <p className={styles.tagline}>{siteConfig.tagline}</p>
        <Link className={styles.cta} to="/wiki/">
          Enter the trail
        </Link>
      </div>

      {stats && (
        <div className={styles.stamp}>
          <span>{stats.articles} ENTRIES</span>
          <span aria-hidden="true">●</span>
          <span>{stats.edges} CROSSINGS</span>
        </div>
      )}
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout title={siteConfig.title} description={siteConfig.tagline}>
      <PosterHero />
    </Layout>
  );
}
