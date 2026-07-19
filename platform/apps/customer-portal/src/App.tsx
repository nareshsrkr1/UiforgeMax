import React from "react";
import { CustomerTable } from "./components/CustomerTable";

export function App() {
  return (
    <main className="portal-app">
      <h1>Customer Portal</h1>
      <CustomerTable />
    </main>
  );
}
