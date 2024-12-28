import { Router } from 'express';
import { getUsers, createUser } from '../controllers/userController';
import { getTasksByUser } from '../controllers/taskController';
import { verifyToken } from '../controllers/authController';


const router = Router();

router.get('/',verifyToken, getUsers);
router.post('/',verifyToken, createUser);
router.get('/:userId/tasks',verifyToken, getTasksByUser);

export default router;