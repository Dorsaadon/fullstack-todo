import React, { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createTask } from '../api';

interface AddTaskProps {
  userId: number | null;
}

const AddTask: React.FC<AddTaskProps> = ({ userId }) => {
  const [taskTitle, setTaskTitle] = useState('');
  const queryClient = useQueryClient();

  const addTaskMutation = useMutation({
    mutationFn: createTask,
    onSuccess: () => {
      queryClient.invalidateQueries({queryKey:['tasks', userId]});
      setTaskTitle('');
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (userId && taskTitle.trim()) {
      addTaskMutation.mutate({ userId, title: taskTitle.trim() });
    }
  };

  if (!userId) return null;

  return (
    <form onSubmit={handleSubmit}>
      <h3>Add a Task</h3>
      <input
        type="text"
        placeholder="Enter task title"
        value={taskTitle}
        onChange={(e) => setTaskTitle(e.target.value)}
      />
      <button type="submit" disabled={addTaskMutation.isPending}>
        {addTaskMutation.isPending ? 'Adding...' : 'Add Task'}
      </button>
    </form>
  );
};

export default AddTask;
