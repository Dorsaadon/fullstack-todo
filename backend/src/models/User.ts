import { DataTypes, Model, Optional } from 'sequelize';
import sequelize from '../database';
import Task from './Task';

// Define the attributes for the User model
interface UserAttributes {
  id: number;
  name: string;
  email: string;
  phoneNumber: string;
  password: string;
}

// Define a type for creating new users, where `id` is optional
interface UserCreationAttributes extends Optional<UserAttributes, 'id'> {}

// Define the User model class
class User extends Model<UserAttributes, UserCreationAttributes> implements UserAttributes {
  public id!: number;
  public name!: string;
  public email!: string;
  public phoneNumber!: string;
  public password!: string;

  // Timestamps
  public readonly createdAt!: Date;
  public readonly updatedAt!: Date;
}

// Initialize the User model with Sequelize
User.init(
  {
    id: {
      type: DataTypes.INTEGER,
      autoIncrement: true,
      primaryKey: true,
    },
    name: {
      type: DataTypes.STRING,
      allowNull: false,
      unique: true,
    },
    email: {
      type: DataTypes.STRING,
      allowNull: true,
    },
    phoneNumber: {
      type: DataTypes.STRING,
      allowNull: true,
    },
    password: {
      type: DataTypes.STRING,
      allowNull: false,
      defaultValue: '1111',
    }
  },
  {
    sequelize,
    modelName: 'User',
  }
);

// Define associations (User has many Tasks)
User.hasMany(Task, {
  foreignKey: 'UserId',
  as: 'tasks',
});
Task.belongsTo(User, {
  foreignKey: 'UserId',
  as: 'user',
});

export default User;