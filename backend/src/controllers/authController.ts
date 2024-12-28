import { Request, Response } from 'express';
import jwt from 'jsonwebtoken';
import bcrypt from 'bcrypt';
import User  from '../models/User';

interface JwtPayload {
    id: string;
}

const SECRET_KEY = 'your_secret_key'; // Replace with a secure secret key

// Register a new user
export const register = async (req: Request, res: Response) => {
    console.log('registering user');
    const { name, email, phoneNumber, password } = req.body;

    if (!name || !email || !phoneNumber || !password) {
        res.status(400).json({ error: 'All fields are required' });
    } else {
        try {
            const hashedPassword = await bcrypt.hash(password, 10);
            const user = await User.create({ name, email, phoneNumber, password: hashedPassword });
            res.status(201).json(user);
        } catch (error) {
            res.status(500).json({ error: 'Failed to register user' });
        }
    }
};

// Login an existing user
export const login = async (req: Request, res: Response) => {
    const { email, password } = req.body;

    if (!email || !password) {
        res.status(400).json({ error: 'Email and password are required' });
    } else {
        try {
            const user = await User.findOne({ where: { email } });
            if (!user) {
            res.status(404).json({ error: 'User not found' });
            } else {

            const isPasswordValid = await bcrypt.compare(password, user.password);
            if (!isPasswordValid) {
            res.status(401).json({ error: 'Invalid password' });
            }

            const token = jwt.sign({ id: user.id }, SECRET_KEY, { expiresIn: '1h' });
            res.status(200).json({ token });
            }
        } catch (error) {
            res.status(500).json({ error: 'Failed to log in' });
        }
    }
};

// Verify token
export const verifyToken = (req: Request, res: Response, next: any) => {
  const token = req.headers['authorization'];
  if (!token) {
    res.status(401).json({ error: 'No token provided' });
  }
  else {
    try {
        const decoded = jwt.verify(token, SECRET_KEY) as JwtPayload;
        (req as any).userId = decoded.id;
        next();
    } catch (error) {
        res.status(403).json({ error: 'Invalid token' });
    }   
  }
};
