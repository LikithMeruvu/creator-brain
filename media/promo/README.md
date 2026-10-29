# Demo video source

The 20-second README demo, built with [HyperFrames](https://hyperframes.heygen.com) (HTML -> video).

```bash
brain graph --no-open            # serves the vault graph on 127.0.0.1:8787
npm install
node build-layout.mjs            # deterministic layout from /graph.json -> layout.js
npx hyperframes render           # -> renders/*.mp4
```
