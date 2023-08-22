const mongoose = require('mongoose');

const { Schema } = mongoose;
const { ObjectId } = mongoose.Types;

const sessionSchema = new Schema({
  "userId": ObjectId(),
  createdAt: { type: Date, default: () => new Date() },
  updatedAt: { type: Date, default: () => new Date() },
  deletedAt: { type: Date, default: null },
});

const Sessions = mongoose.model('Sessions', sessionSchema);

module.exports = Sessions;
