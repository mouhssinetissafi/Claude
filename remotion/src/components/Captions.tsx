import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import type {CaptionSegment, Captions as CaptionsDoc} from '../types';
import type {Theme} from '../themes';

const findSegment = (segments: CaptionSegment[], t: number): CaptionSegment | null => {
  for (const seg of segments) {
    if (t >= seg.start - 0.05 && t <= seg.end + 0.12) {
      return seg;
    }
  }
  return null;
};

export const Captions: React.FC<{captions: CaptionsDoc; theme: Theme}> = ({captions, theme}) => {
  const frame = useCurrentFrame();
  const {fps, height, width} = useVideoConfig();
  const t = frame / fps;
  const segment = findSegment(captions.segments, t);
  if (!segment) {
    return null;
  }
  const segmentStartFrame = Math.round(segment.start * fps);
  const pop = spring({frame: frame - segmentStartFrame, fps, config: {damping: 14, stiffness: 180, mass: 0.6}});
  const entrance = interpolate(pop, [0, 1], [0.86, 1]);
  const c = theme.caption;

  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', pointerEvents: 'none'}}>
      <div
        style={{
          position: 'absolute',
          bottom: Math.max(c.bottomOffset, theme.safeBottom + 40),
          left: (width - c.maxWidth) / 2,
          width: c.maxWidth,
          display: 'flex',
          flexWrap: 'wrap',
          justifyContent: 'center',
          alignItems: 'center',
          gap: '0 18px',
          transform: `scale(${entrance})`,
          transformOrigin: 'center bottom',
          fontFamily: theme.fontFamily,
          fontWeight: c.fontWeight,
          fontSize: c.fontSize,
          lineHeight: 1.15,
          textAlign: 'center',
          textTransform: c.uppercase ? 'uppercase' : 'none',
          maxHeight: height * 0.3,
        }}
      >
        {segment.words.map((w, i) => {
          const active = t >= w.start - 0.02 && t <= w.end + 0.08;
          const color = w.emphasis ? c.emphasisColor : active ? c.activeColor : c.color;
          const scale = active ? 1.08 : 1;
          return (
            <span
              key={`${i}-${w.start}`}
              style={{
                color,
                display: 'inline-block',
                transform: `scale(${scale})`,
                WebkitTextStroke: `${c.strokeWidth}px ${c.strokeColor}`,
                paintOrder: 'stroke fill',
                textShadow: c.shadow,
                transition: 'none',
              }}
            >
              {w.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
