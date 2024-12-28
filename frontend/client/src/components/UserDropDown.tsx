import React from 'react';

interface UserDropdownProps {
  users: { id: number; name: string }[];
  isLoading: boolean;
  selectedUser: number | null;
  setSelectedUser: (id: number) => void;
}

const UserDropdown: React.FC<UserDropdownProps> = ({
  users,
  isLoading,
  selectedUser,
  setSelectedUser,
}) => {
  if (isLoading) {
    return <p>Loading users...</p>;
  }

  return (
    <select
      onChange={(e) => setSelectedUser(Number(e.target.value))}
      value={selectedUser || ''}
    >
      <option value="" disabled>
        Select a User
      </option>
      {users.map((user) => (
        <option key={user.id} value={user.id}>
          {user.name}
        </option>
      ))}
    </select>
  );
};

export default UserDropdown;
