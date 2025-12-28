import { Pool } from "pg";
import { postgres_host, postgres_user } from "../config.js";

export const pool = new Pool({
  connectionString: "postgres://node_auth:password@localhost:5432/neemba",
});

export async function checkUser() {
  const { rows } = await pool.query("SELECT current_user, session_user;");
  console.log(rows);
}
