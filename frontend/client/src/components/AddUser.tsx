import React, { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createUser } from '../api';

const AddUser: React.FC = () => {
  const [formData, setFormData] = useState({
    name: '',
    email: '',
    phoneNumber: '',
  });

  const queryClient = useQueryClient();
  const addUserMutation = useMutation({
    mutationFn: createUser,
    onSuccess: () => {
      queryClient.invalidateQueries({queryKey:['users']});
      setFormData({ name: '', email: '', phoneNumber: '' });
    },
  });

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    const { name, email, phoneNumber } = formData;
    if (!name || !email || !phoneNumber) {
      alert('All fields are required.');
      return;
    }

    addUserMutation.mutate(formData);
  };

  return (
    <form onSubmit={handleSubmit}>
      <h3>Add User</h3>
      <input
        type="text"
        name="name"
        placeholder="Name"
        value={formData.name}
        onChange={handleChange}
        required
      />
      <input
        type="email"
        name="email"
        placeholder="Email"
        value={formData.email}
        onChange={handleChange}
        required
      />
      <input
        type="text"
        name="phoneNumber"
        placeholder="Phone Number"
        value={formData.phoneNumber}
        onChange={handleChange}
        required
      />
      <button type="submit" disabled={addUserMutation.isPending}>
        {addUserMutation.isPending ? 'Adding...' : 'Add User'}
      </button>
    </form>
  );
};

export default AddUser;
