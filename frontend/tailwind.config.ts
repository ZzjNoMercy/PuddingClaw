import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        klein: {
          50: "#eef2ff",
          100: "#dde4ff",
          500: "#002fa7",
          600: "#002590",
          700: "#001b70",
        },
        glass: {
          white: "rgba(255, 255, 255, 0.72)",
          border: "rgba(0, 0, 0, 0.06)",
        },
      },
      backdropBlur: {
        glass: "20px",
      },
    },
  },
  plugins: [],
};

export default config;
