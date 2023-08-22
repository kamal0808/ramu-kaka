const mongoose = require('mongoose');

const { Schema } = mongoose;

const userSchema = new Schema({
  username: { type: String, required: true },
  createdAt: { type: Date, default: () => new Date() },
  updatedAt: { type: Date, default: () => new Date() },
  deletedAt: { type: Date, default: null },
});

const Users = mongoose.model('Users', userSchema);

module.exports = Users;
