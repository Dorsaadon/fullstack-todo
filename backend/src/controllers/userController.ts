import { Request, Response } from 'express';
import User from '../models/User';

export const getUsers = async (req: Request, res: Response) => {
  const users = await User.findAll();
  res.json(users);
};

export const createUser = async (req: Request, res: Response) => {
    try {
        console.log('creating user');
        const { name, email, phoneNumber, password } = req.body;
        if (!name || !email || !phoneNumber || password) {
            res.status(400).json({ error: 'Name is required' });
        } else { 
            const user = await User.create({ name, email, phoneNumber, password });
            res.status(201).json(user);
        }
      } catch (error) {
        console.error(error);
        res.status(500).json({ error: 'Failed to create user' });
      }
};