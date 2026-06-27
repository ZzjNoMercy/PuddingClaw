/** @type {import('next').NextConfig} */
const backendUrl = process.env.BACKEND_INTERNAL_URL || 'http://localhost:8888';

const nextConfig = {
  output: 'standalone',
  // Keep `next dev` and `next build` from fighting over the same `.next`
  // directory. The dev server owns `.next`; verification/production builds can
  // set NEXT_DIST_DIR=.next-build so they do not invalidate hot-reload chunks.
  distDir: process.env.NEXT_DIST_DIR || '.next',
  // Next's gzip compressor buffers proxied text/event-stream responses until
  // the compression block is flushed. Browsers advertise Accept-Encoding by
  // default, so SSE appeared non-streaming even though curl without compression
  // received incremental chunks. Disable application-level compression; static
  // assets are already pre-compressed/cacheable and SSE must remain unbuffered.
  compress: false,
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${backendUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
