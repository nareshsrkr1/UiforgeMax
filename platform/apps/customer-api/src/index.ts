import express from "express";
import { customersRouter } from "./routes/customers";

const app = express();
app.use(express.json());
app.use("/api/customers", customersRouter);

const port = process.env.PORT || 4000;
app.listen(port, () => {
  // eslint-disable-next-line no-console
  console.log(`customer-api listening on ${port}`);
});

export { app };
