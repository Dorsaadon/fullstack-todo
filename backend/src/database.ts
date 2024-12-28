import { Sequelize } from 'sequelize';

// Initialize SQLite database
const sequelize = new Sequelize({
  dialect: 'sqlite',
  storage: './todo.db',
});

export default sequelize;