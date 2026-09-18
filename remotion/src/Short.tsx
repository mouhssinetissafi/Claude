import React from 'react';
import {AbsoluteFill} from 'remotion';
import type {ShortProps} from './types';
import {getTheme} from './themes';
import {MediaLayer} from './components/MediaLayer';
import {Captions} from './components/Captions';
import {Overlay} from './components/Overlay';
import {ProgressBar} from './components/ProgressBar';
import {AudioTracks} from './components/AudioTracks';

/**
 * The single composition used by both pipeline modes. Everything it needs is in
 * the props: a resolved timeline (what to show when), word-level captions,
 * the script summary (overlay/emphasis) and a theme name.
 */
export const Short: React.FC<ShortProps> = ({assetBase, theme: themeName, timeline, captions}) => {
  const theme = getTheme(themeName);
  return (
    <AbsoluteFill style={{background: theme.background, fontFamily: theme.fontFamily}}>
      <MediaLayer timeline={timeline} assetBase={assetBase} theme={theme} />
      <Overlay timeline={timeline} theme={theme} />
      <Captions captions={captions} theme={theme} />
      <ProgressBar theme={theme} />
      <AudioTracks timeline={timeline} assetBase={assetBase} />
    </AbsoluteFill>
  );
};
