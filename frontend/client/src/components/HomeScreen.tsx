import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchUsers, fetchTasks } from '../api';
import UserDropdown from './UserDropDown';
import TaskList from './TaskList';
import AddTask from './AddTask';
import styles from '../styles/HomeScreen.module.scss';
import Header from './Header';

const HomeScreen: React.FC = () => {
  const [selectedUser, setSelectedUser] = useState<number | null>(null);

  const { data: users = [], isLoading: isUsersLoading } = useQuery({
    queryKey: ['users'],
    queryFn: fetchUsers,
  });

  const { data: tasks, isLoading: isTasksLoading } = useQuery({
    queryKey: ['tasks', selectedUser],
    queryFn: () => fetchTasks(selectedUser!),
    enabled: !!selectedUser,
  });

  return (
    <div className={styles.homeScreenContainer}>
          <Header></Header>
      <h1 className={styles.title}>To-Do List</h1>
      <UserDropdown
        users={users}
        isLoading={isUsersLoading}
        selectedUser={selectedUser}
        setSelectedUser={setSelectedUser}
      />
      {selectedUser && (
        <>
          <TaskList tasks={tasks || []} isLoading={isTasksLoading} />
          <AddTask userId={selectedUser} />
        </>
      )}
    </div>
  );
};

export default HomeScreen;
