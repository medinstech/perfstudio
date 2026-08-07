// Domain model and the command bus every mutation flows through.
export * from './model.js';
export * from './command.js';
export * from './commands.js';

// Geometry is the single source of truth for hole addressing and placement maths.
export * from './geometry.js';

// What the board actually connects, and how that compares to the schematic.
export * from './connectivity.js';
export * from './lvs.js';
export * from './drc.js';

// Parts and files.
export * from './footprints.js';
export * from './persist.js';
