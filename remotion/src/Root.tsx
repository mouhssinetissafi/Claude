import React from 'react';
import {Composition} from 'remotion';
import {Short} from './Short';
import type {ShortProps} from './types';

const PREVIEW_IMAGE =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1920"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b2a49"/><stop offset="1" stop-color="#0b1020"/></linearGradient></defs><rect width="1080" height="1920" fill="url(#g)"/><circle cx="540" cy="900" r="260" fill="#ffd60a" opacity="0.85"/></svg>',
  );

/** Props used by Remotion Studio when no job props are supplied. Real renders pass --props. */
export const previewProps: ShortProps = {
  jobId: 'preview',
  assetBase: 'jobs/preview/',
  theme: 'default',
  timeline: {
    version: 1,
    fps: 30,
    width: 1080,
    height: 1920,
    duration: 6,
    voice: '',
    music: null,
    sfx: [],
    watermark: null,
    lines: [
      {
        line_id: 1,
        start: 0,
        end: 3,
        overlay_text: 'PREVIEW',
        emphasis_words: ['timeline'],
        segments: [{src: PREVIEW_IMAGE, type: 'image', start: 0, end: 3, effect: 'kenburns', transition: 'cut', scene_id: null}],
      },
      {
        line_id: 2,
        start: 3,
        end: 6,
        overlay_text: null,
        emphasis_words: [],
        segments: [{src: PREVIEW_IMAGE, type: 'image', start: 3, end: 6, effect: 'kenburns', transition: 'fade', scene_id: null}],
      },
    ],
  },
  captions: {
    version: 1,
    language: 'en',
    duration: 6,
    words: [],
    segments: [
      {
        text: 'This is the timeline preview',
        start: 0.2,
        end: 2.8,
        words: [
          {text: 'This', start: 0.2, end: 0.6, line_id: 1},
          {text: 'is', start: 0.6, end: 0.9, line_id: 1},
          {text: 'the', start: 0.9, end: 1.2, line_id: 1},
          {text: 'timeline', start: 1.2, end: 2.0, line_id: 1, emphasis: true},
          {text: 'preview', start: 2.0, end: 2.8, line_id: 1},
        ],
      },
      {
        text: 'rendered by Remotion',
        start: 3.2,
        end: 5.5,
        words: [
          {text: 'rendered', start: 3.2, end: 4.0, line_id: 2},
          {text: 'by', start: 4.0, end: 4.4, line_id: 2},
          {text: 'Remotion', start: 4.4, end: 5.5, line_id: 2},
        ],
      },
    ],
  },
  script: {
    title: 'Preview',
    lines: [
      {id: 1, narration: 'This is the timeline preview', overlay_text: 'PREVIEW', emphasis_words: ['timeline']},
      {id: 2, narration: 'rendered by Remotion', overlay_text: null, emphasis_words: []},
    ],
  },
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Short"
      component={Short}
      durationInFrames={Math.ceil(previewProps.timeline.duration * previewProps.timeline.fps)}
      fps={previewProps.timeline.fps}
      width={previewProps.timeline.width}
      height={previewProps.timeline.height}
      defaultProps={previewProps}
      calculateMetadata={({props}) => {
        const tl = props.timeline;
        return {
          durationInFrames: Math.max(1, Math.ceil(tl.duration * tl.fps)),
          fps: tl.fps,
          width: tl.width,
          height: tl.height,
        };
      }}
    />
  );
};
