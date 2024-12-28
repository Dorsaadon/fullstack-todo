import { DataTypes, Model, Optional } from 'sequelize';
import sequelize from '../database';

// Define the attributes for the Task model
interface TaskAttributes {
  id: number;
  title: string;
  completed?: boolean;
  UserId: number; // Foreign key for the User association
}

// Define a type for creating new tasks, where `id` is optional
interface TaskCreationAttributes extends Optional<TaskAttributes, 'id'> {}

// Define the Task model class
class Task extends Model<TaskAttributes, TaskCreationAttributes> implements TaskAttributes {
  public id!: number;
  public title!: string;
  public completed!: boolean;
  public UserId!: number; // Foreign key for the User association

  // Timestamps
  public readonly createdAt!: Date;
  public readonly updatedAt!: Date;
}

// Initialize the Task model with Sequelize
Task.init(
  {
    id: {
      type: DataTypes.INTEGER,
      autoIncrement: true,
      primaryKey: true,
    },
    title: {
      type: DataTypes.STRING,
      allowNull: false,
    },
    completed: {
      type: DataTypes.BOOLEAN,
      allowNull: false,
      defaultValue: false, // Default to false
    },
    UserId: {
      type: DataTypes.INTEGER,
      allowNull: false,
    },
  },
  {
    sequelize,
    modelName: 'Task',
  }
);

export default Task;