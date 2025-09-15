export const insertChurchQuery = `
    INSERT INTO auth.churches (name, address, phone_number)
    VALUES ($1, $2, $3)
    RETURNING church_id, name, address, phone_number, created_at;
`;

export const getChurchQuery = `
    SELECT church_id, name, address, phone_number
    FROM auth.churches
    WHERE church_id = $1;
`;
