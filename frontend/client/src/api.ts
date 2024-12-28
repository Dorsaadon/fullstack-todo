import axios from 'axios';

const API = axios.create({ baseURL: 'http://localhost:5001/api' });

API.interceptors.request.use((config) => {
  const token = localStorage.getItem('Authorization');
  if (token) {
    config.headers['Authorization'] = token;
  }
  return config;
});

export const fetchUsers = async () => {
    try {
        console.log('fetching users');
        const response = await API.get('/users');
        return response.data;
    } catch (error) {
      console.error('Error fetching users:', error);
      throw new Error('Failed to fetch users');
    }
};
export const createUser = async ({name, email, phoneNumber}: {name:string; email:string; phoneNumber:string} ) => {
    try {
        console.log('creating user');
        const response = await API.post('/users', {name,email,phoneNumber});
        return response.data;
    } catch (error) {
        console.error('error creating user:', error);
        throw new Error('Failed to create user');
    }
}

export const fetchTasks = async (userId: number) =>
  (await API.get(`/users/${userId}/tasks`)).data;

export const createTask = async ({ userId, title }: { userId: number; title: string }) => {
  const response = await API.post('/tasks', { userId, title, completed: false });
  return response.data;
};


export const updateTask = async ({ id, completed }: { id: number; completed: boolean }) => {
  const response = await API.patch(`/tasks/${id}`, { completed });  
  return response.data;
};

export const deleteTask = async (taskId: number) =>
  (await API.delete(`/tasks/${taskId}`)).data;

export const loginUser = async (data: {email:string; password:string;}) => {
  const response = await API.post('/auth/login', data);
  return response.data;
};

export const registerUser = async (data: { name: string; email: string; phoneNumber: string; password: string }) => {
  const response = await API.post('/auth/register', data);
  return response.data;
};