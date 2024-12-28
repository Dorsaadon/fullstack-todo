import express from 'express';
import { verifyToken } from '../controllers/authController';
import {
  createTask,
  updateTask,
  deleteTask,
} from '../controllers/taskController';

const router = express.Router();

router.post('/',verifyToken, createTask);
router.patch('/:taskId',verifyToken, updateTask);
router.delete('/:taskId',verifyToken, deleteTask);

export default router;