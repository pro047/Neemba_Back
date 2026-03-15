import { createApp } from "./app.js";

const port = 3000;
const { server } = createApp();

server.listen(port, "0.0.0.0", () => {
  console.log(`Node server connected port : ${port}`);
});
