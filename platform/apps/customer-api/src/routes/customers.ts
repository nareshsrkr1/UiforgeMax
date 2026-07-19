import { Router } from "express";

interface Customer {
  id: string;
  name: string;
  email: string;
  status: string;
}

const customers: Customer[] = [
  { id: "1", name: "Ada Lovelace", email: "ada@example.com", status: "active" },
  { id: "2", name: "Grace Hopper", email: "grace@example.com", status: "active" },
];

export const customersRouter = Router();

customersRouter.get("/", (_req, res) => {
  res.json(customers);
});

customersRouter.get("/:id", (req, res) => {
  const customer = customers.find((c) => c.id === req.params.id);
  if (!customer) {
    res.status(404).json({ error: "not found" });
    return;
  }
  res.json(customer);
});
