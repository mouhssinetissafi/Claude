import React from 'react';
import {Img, useVideoConfig} from 'remotion';
import type {TimelineWatermark} from '../types';
import type {Theme} from '../themes';
import {resolveAsset} from '../lib/assets';

/** Reserved box (px) for the logo so other layers can stay clear of it. */
export const watermarkBox = (wm: TimelineWatermark, width: number, height: number): {w: number; h: number} => ({
  w: Math.round(width * wm.width_fraction),
  h: Math.round(height * wm.max_height_fraction),
});

/** Small, subtle logo inside the safe zones. Transparency of the PNG is preserved. */
export const Watermark: React.FC<{watermark: TimelineWatermark | null | undefined; assetBase: string; theme: Theme}> = ({watermark, assetBase, theme}) => {
  const {width, height} = useVideoConfig();
  if (!watermark || !watermark.src) {
    return null;
  }
  const {w, h} = watermarkBox(watermark, width, height);
  const margin = watermark.margin ?? 24;
  const pos = watermark.position ?? 'top-right';
  const vertical: React.CSSProperties = pos.startsWith('top') ? {top: theme.safeTop + margin} : {bottom: theme.safeBottom + margin};
  const horizontal: React.CSSProperties = pos.endsWith('left') ? {left: theme.safeSide + margin} : {right: theme.safeSide + margin};
  return (
    <div
      style={{
        position: 'absolute',
        ...vertical,
        ...horizontal,
        width: w,
        height: h,
        display: 'flex',
        alignItems: pos.startsWith('top') ? 'flex-start' : 'flex-end',
        justifyContent: pos.endsWith('left') ? 'flex-start' : 'flex-end',
        pointerEvents: 'none',
        opacity: watermark.opacity ?? 0.85,
      }}
    >
      <Img src={resolveAsset(assetBase, watermark.src)} style={{maxWidth: w, maxHeight: h, objectFit: 'contain'}} />
    </div>
  );
};
