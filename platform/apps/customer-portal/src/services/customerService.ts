export interface Customer {
  id: string;
  name: string;
  email: string;
  status: string;
}

export async function fetchCustomers(): Promise<Customer[]> {
  const res = await fetch("/api/customers");
  if (!res.ok) {
    return [];
  }
  return res.json();
}
