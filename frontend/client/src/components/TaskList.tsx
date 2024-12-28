import React from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateTask, deleteTask } from '../api';
import styles from '../styles/TaskList.module.scss';

interface Task {
  id: number;
  title: string;
  completed: boolean;
}

interface TaskListProps {
  tasks: Task[];
  isLoading: boolean;
}

const TaskList: React.FC<TaskListProps> = ({ tasks, isLoading }) => {
  const queryClient = useQueryClient();

  const updateTaskMutation = useMutation({
    mutationFn: updateTask,
    onSuccess: () => queryClient.invalidateQueries({queryKey:['tasks']}),
  });

  const deleteTaskMutation = useMutation({
    mutationFn: deleteTask,
    onSuccess: () => queryClient.invalidateQueries({queryKey:['tasks']}),
  });

  if (isLoading) {
    return <p>Loading tasks...</p>;
  }

  if (tasks.length === 0) {
    return <p>No tasks found for this user.</p>;
  }

  return (
    <ul className={styles.taskListContainer}>
      {tasks.map((task) => (
        <li className={styles.taskItem} key={task.id}>
          <input
            className={styles.taskActions}
            type="checkbox"
            checked={task.completed}
            onChange={() =>
              updateTaskMutation.mutate({ id: task.id, completed: !task.completed })
            }
          />
          <strong className={styles.taskDetails}>{task.title}</strong>
          <button className={styles.delete} onClick={() => deleteTaskMutation.mutate(task.id)}>Delete</button>
        </li>
      ))}
    </ul>
  );
};

export default TaskList;
