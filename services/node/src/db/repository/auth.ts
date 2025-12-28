import { checkUser, pool } from "../pool.js";
import { getChurchQuery, insertChurchQuery } from "../sql/churches.js";

class ChurchAuth {
  async getById(churchId: number) {
    const { rows } = await pool.query(getChurchQuery, [churchId]);
    return rows;
  }

  async create(input: { name: string; address: string; phoneNumber: string }) {
    const { rows } = await pool.query(insertChurchQuery, [
      input.name,
      input.address,
      input.phoneNumber,
    ]);

    return rows[0];
  }
}

checkUser();

const auth = new ChurchAuth();

const create = await auth.create({
  name: "name",
  address: "adr",
  phoneNumber: "phone",
});

console.log("create:", create);
