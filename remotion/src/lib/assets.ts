import {staticFile} from 'remotion';

/** Resolve a timeline src against the job asset base; absolute/data URLs pass through. */
export const resolveAsset = (assetBase: string, src: string): string => {
  if (/^(https?:|data:|blob:)/.test(src)) {
    return src;
  }
  const base = assetBase.endsWith('/') ? assetBase : `${assetBase}/`;
  return staticFile(`${base}${src.replace(/^\/+/, '')}`);
};

export const secondsToFrames = (seconds: number, fps: number): number => Math.round(seconds * fps);
