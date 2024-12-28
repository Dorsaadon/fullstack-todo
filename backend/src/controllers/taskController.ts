import { Request, Response } from 'express';
import Task from '../models/Task';

export const getTasksByUser = async (req: Request, res: Response) => {
  const { userId } = req.params;
  const tasks = await Task.findAll({ where: { UserId: userId } });
  res.json(tasks);
};

export const createTask = async (req: Request, res: Response) => {
  const { userId, title, completed } = req.body;

  if (!userId || !title) {
    res.status(400).json({ error: 'User ID and title are required' });
  } else {
    try {
      const task = await Task.create({ UserId: userId, title:title, completed: completed || false });
      res.status(201).json(task);
    } catch (error) {
      console.error(error);
      res.status(500).json({ error: 'Failed to create task' });
    }
  }
};


export const updateTask = async (req: Request, res: Response) => {
  const { taskId } = req.params;
  const { title: title, completed: completed } = req.body;
  const task = await Task.findByPk(taskId);
  if (task) {
    task.completed = completed;
    await task.save();
    res.json(task);
  } else {
    res.status(404).send('Task not found');
  }
};

export const deleteTask = async (req: Request, res: Response) => {
  const { taskId } = req.params;
  const task = await Task.findByPk(taskId);
  if (task) {
    await task.destroy();
    res.sendStatus(204);
  } else {
    res.status(404).send('Task not found');
  }
};