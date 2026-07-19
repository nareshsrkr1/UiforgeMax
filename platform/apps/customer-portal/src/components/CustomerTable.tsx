import React, { useState } from "react";
import { fetchCustomers, type Customer } from "../services/customerService";

export function CustomerTable() {
  const [customers, setCustomers] = useState<Customer[]>([]);

  React.useEffect(() => {
    fetchCustomers().then(setCustomers);
  }, []);

  return (
    <table className="customer-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Email</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {customers.map((c) => (
          <tr key={c.id}>
            <td>{c.name}</td>
            <td>{c.email}</td>
            <td>{c.status}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
