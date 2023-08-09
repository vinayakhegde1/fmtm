import {
  Card,
  CardContent,
  Typography,
  Stack,
  Button,
  InputBase,
  Input,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Slide,
  IconButton,
  Tabs,
  Tab,
  Divider,
  List,
  ListItem,
  ListItemText,
  Menu,
  Alert as MuiAlert,
  Snackbar,
  AppBar,
  Toolbar,
  Grid,
  Pagination,
  CssBaseline,
  Paper,
  Container,
  TextField,
  FormControlLabel,
  Select,
  MenuItem,
  FormControl,
  FormLabel,
  FormGroup,
  Box,
  Avatar,
  InputAdornment,
  InputLabel,
  Tooltip,
  Breadcrumbs,
  CardMedia,
} from '@mui/material';
import { LoadingButton } from '@mui/lab';
import { createTheme, ThemeProvider } from '@mui/material/styles';
import axios from 'axios';
import { PersistGate } from 'redux-persist/integration/react';
// import { Navigation as SwiperNavigation, Pagination as SwiperPagination } from "swiper";
// import { Swiper, SwiperSlide } from 'swiper/react';
import Skeleton, { SkeletonTheme } from 'react-loading-skeleton';
import {
  useNavigate,
  useParams,
  Link,
  Outlet,
  RouterProvider,
  useLocation,
  createBrowserRouter,
} from 'react-router-dom';
import { useSelector, useDispatch, Provider } from 'react-redux';
import { createSlice, configureStore, getDefaultMiddleware } from '@reduxjs/toolkit';
import { combineReducers } from 'redux';
import LoadingBar from '../components/createproject/LoadingBar';
import { TaskActions } from '../store/slices/TaskSlice';
import CustomizedModal from '../utilities/CustomizedModal';

export default {
  Provider,
  PersistGate,
  RouterProvider,
  createBrowserRouter,
  Card,
  CardContent,
  useNavigate,
  useParams,
  useSelector,
  useDispatch,
  Stack,
  Typography,
  Button,
  InputBase,
  Input,
  Skeleton,
  SkeletonTheme,
  createSlice,
  configureStore,
  combineReducers,
  useLocation,
  getDefaultMiddleware,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Slide,
  IconButton,
  Tabs,
  Tab,
  Divider,
  List,
  ListItem,
  ListItemText,
  Menu,
  MuiAlert,
  Snackbar,
  AppBar,
  Toolbar,
  Link,
  Grid,
  Pagination,
  ThemeProvider,
  CssBaseline,
  Paper,
  createTheme,
  Outlet,
  Container,
  // SwiperNavigation,
  // SwiperPagination,
  // Swiper,
  // SwiperSlide,
  axios,
  TextField,
  FormControlLabel,
  Select,
  MenuItem,
  FormControl,
  FormLabel,
  FormGroup,
  Box,
  Avatar,
  InputAdornment,
  InputLabel,
  Tooltip,
  Breadcrumbs,
  CardMedia,
  LoadingBar,
  TaskActions,
  LoadingButton,
  CustomizedModal,
};
