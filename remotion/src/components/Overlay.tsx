import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import type {Timeline} from '../types';
import type {Theme} from '../themes';

/** On-screen overlay_text for the current narration line (top third, inside safe zones). */
export const Overlay: React.FC<{timeline: Timeline; theme: Theme}> = ({timeline, theme}) => {
  const frame = useCurrentFrame();
  const {fps, width} = useVideoConfig();
  const t = frame / fps;
  const line = timeline.lines.find((l) => t >= l.start && t < l.end && l.overlay_text);
  if (!line || !line.overlay_text) {
    return null;
  }
  const startFrame = Math.round(line.start * fps);
  const endFrame = Math.round(line.end * fps);
  const holdFrames = Math.min(endFrame - startFrame, Math.round(fps * 2.6));
  const local = frame - startFrame;
  if (local > holdFrames) {
    return null;
  }
  const enter = spring({frame: local, fps, config: {damping: 12, stiffness: 160, mass: 0.7}});
  const exit = interpolate(local, [holdFrames - Math.round(fps * 0.25), holdFrames], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const o = theme.overlay;
  return (
    <AbsoluteFill style={{alignItems: 'center', pointerEvents: 'none'}}>
      <div
        style={{
          position: 'absolute',
          top: Math.max(o.topOffset, theme.safeTop + 40),
          maxWidth: width - theme.safeSide * 2,
          padding: '18px 40px',
          borderRadius: o.borderRadius,
          background: o.background,
          color: o.color,
          fontFamily: theme.fontFamily,
          fontWeight: o.fontWeight,
          fontSize: o.fontSize,
          letterSpacing: 2,
          textTransform: 'uppercase',
          textAlign: 'center',
          opacity: Math.min(enter, exit),
          transform: `scale(${interpolate(enter, [0, 1], [0.6, 1])}) rotate(${interpolate(enter, [0, 1], [-3, 0])}deg)`,
          boxShadow: '0 12px 40px rgba(0,0,0,0.45)',
        }}
      >
        {line.overlay_text}
      </div>
    </AbsoluteFill>
  );
};
