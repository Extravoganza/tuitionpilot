# Deploy the demo site to Vercel

The site is static: `demo/index.html` plus `demo/assets/`. No build step.

1. Push this repository to GitHub (github.com/Extravoganza/tuitionpilot).
2. Open https://vercel.com/new, sign in with GitHub, choose **Import** next to `tuitionpilot`.
3. Leave every setting as it is: `vercel.json` already sets Framework = Other, no install or build command, output directory `demo`.
4. Press **Deploy**. After about 30 seconds you get a public link like `https://tuitionpilot.vercel.app`.

Every new push to `main` redeploys automatically.

To rebuild the page from the engine first: `python tools/build_demo.py` (writes `demo/index.html`).
