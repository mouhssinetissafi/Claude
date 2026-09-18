import React from 'react';
import {AbsoluteFill, useVideoConfig} from 'remotion';
import type {ShortProps} from './types';
import {getTheme} from './themes';
import {MediaLayer} from './components/MediaLayer';
import {Captions} from './components/Captions';
import {Overlay} from './components/Overlay';
import {ProgressBar} from './components/ProgressBar';
import {AudioTracks} from './components/AudioTracks';
import {Watermark, watermarkBox} from './components/Watermark';

/**
 * The single composition used by both pipeline modes. Everything it needs is in
 * the props: a resolved timeline (what to show when), word-level captions,
 * the script summary (overlay/emphasis), an optional watermark and a theme name.
 */
export const Short: React.FC<ShortProps> = ({assetBase, theme: themeName, timeline, captions}) => {
  const theme = getTheme(themeName);
  const {width, height} = useVideoConfig();
  const watermark = timeline.watermark ?? null;
  // Keep the overlay pill clear of a top-positioned watermark so text is never covered.
  const overlayInset = watermark && watermark.position.startsWith('top') ? watermarkBox(watermark, width, height).h + (watermark.margin ?? 24) : 0;
  return (
    <AbsoluteFill style={{background: theme.background, fontFamily: theme.fontFamily}}>
      <MediaLayer timeline={timeline} assetBase={assetBase} theme={theme} />
      <Overlay timeline={timeline} theme={theme} topInset={overlayInset} />
      <Captions captions={captions} theme={theme} />
      <Watermark watermark={watermark} assetBase={assetBase} theme={theme} />
      <ProgressBar theme={theme} />
      <AudioTracks timeline={timeline} assetBase={assetBase} />
    </AbsoluteFill>
  );
};
