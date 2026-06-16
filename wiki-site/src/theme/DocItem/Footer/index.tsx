import type {ReactNode} from 'react';
import Footer from '@theme-original/DocItem/Footer';
import {useDoc} from '@docusaurus/plugin-content-docs/client';
import {Backlink} from 'docusaurus-plugin-backlinks/components';

export default function FooterWrapper(): ReactNode {
  const {metadata} = useDoc();
  return (
    <>
      <Backlink documentPath={metadata.permalink} />
      <Footer />
    </>
  );
}
